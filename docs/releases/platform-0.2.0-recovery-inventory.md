# Platform 0.2.0 Recovery Inventory (COV-5)

Status: record of what entered `main` through the repository recovery, and the
agreed merge and rollback strategy. Companion to the
[RC.1 release baseline](platform-0.2.0-rc.1.md).

## Delivery Record

| Item | Value |
| --- | --- |
| Recovery PR | [#2](https://github.com/chijiang/Covalent/pull/2), merged 2026-10-03 |
| Merge commit | `ef42a5c` (squash, single parent on rewritten `main`) |
| Stabilization branch tip | `25cedaa` — tree identical to `ef42a5c` (tree `3a99c0a7`) |
| Required checks | All green on the PR head and again on `main` after merge |
| Working tree at merge | Clean; no uncommitted changes pending |

Deviation note: the [release policy](../versioning-and-release.md) §8 reserved a
merge-commit exception for this recovery PR to preserve branch topology. The PR
was squash-merged instead, so the 144-commit release-line history did not enter
`main` as individual commits. The net content is identical (matching tree
SHAs), and the squash result is a single revertible unit. All subsequent work
starts from `main` on compliant short-lived branches.

## Change Inventory

594 files changed, +78,838 / −17,919, distributed as:

| Area | Files | Highlights |
| --- | --- | --- |
| `products/enterprise` | 258 | Modular FastAPI app split, console UI, auth pages, audit statistics, public agent discovery |
| `products/desktop` | 83 | Electron/React shell + Python sidecar, session management, skill/MCP management, NDJSON streaming, macOS packaging scripts |
| `packages/python` | 79 | `agent_framework` renamed to `covalent` namespaces; new `covalent-agent-kit` with multi-provider model adapters |
| `tests` | 68 | Shared backend suite (769+ tests, real PostgreSQL), config bundle CLI tests |
| `docs` | 28 | Branching/versioning policy, monorepo architecture, design specs, RC.1 baseline |
| `products/lite` | 8 | Installable package scaffold (no runnable release) |
| `tooling` | 2 | `verify_wheels.py` isolated wheel verification |

Feature lines delivered:

1. **Monorepo restructure and namespace migration** — legacy `covalent.*`
   namespace removed, `frontend/` moved into `products/enterprise/web`,
   `agent_framework` split into `covalent_enterprise`, `covalent_runtime`,
   `covalent_contracts`, `covalent_agent_kit`, and per-product packages.
2. **Durable runs and token streaming** — `chat_runs` persistence, token-level
   deltas, reconnect, explicit cancellation, terminal recovery after restart.
3. **Stateful delegates** — full lifecycle (spawn, lease, TTL recovery, HITL
   escalation, SSE events) behind
   `AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED=false` (opt-in, rollout in
   [COV-23](https://linear.app/covalent-app/issue/COV-23/)).
4. **Per-agent sandbox profiles** — profile/instance schema, image library
   (polyglot, slim, toolbox, datascience), confinement hardening; real-Docker
   acceptance tracked in
   [COV-22](https://linear.app/covalent-app/issue/COV-22/).
5. **Desktop product line** — runnable local loop with sidecar handshake,
   built-in skill sync, and session/activity persistence.
6. **Provider expansion** — APIH provider, `api_style` support, OpenAI/Anthropic
   compatible and OpenAI Responses adapters.
7. **Config bundle CLI** — `covalent config export/import` with natural keys,
   row-level upsert, and plaintext-key warnings.
8. **Platform governance** — branching/versioning policy, CI workflows
   (`enterprise.yml`, `desktop.yml`), full-matrix gates, isolated wheel checks.

## Database Migrations

Nine new Enterprise migrations entered with this recovery (heads at
`20260921_000033_add_provider_api_style`):

| Revision | Change |
| --- | --- |
| `20260815_000025` | Sandbox profiles and instances |
| `20260815_000026` | Sandbox profile default unique index |
| `20260818_000027` | Delegate runs and delegate messages |
| `20260826_000028` | Chat runs |
| `20260828_000029` | Agent context window |
| `20260906_000030` | Provider APIH fields |
| `20260911_000031` | Chat messages reasoning |
| `20260914_000032` | Split chat activity raw payload |
| `20260921_000033` | Provider API style |

Operational breaking change: the server no longer applies schema migrations
during startup. Run `uv run python main.py migrate` explicitly before
replacing application replicas.

## Risk Couplings and Rollback

- **Rollback point**: revert the single merge commit `ef42a5c`. Because the
  recovery landed as one squash commit, this is a single revertible unit on
  `main`.
- **Do not roll back the database.** Migrations 000025–000033 have no reviewed
  downgrade paths; back up the database before applying and keep forward-only.
- **Namespace migration coupling**: the `agent_framework` → `covalent` rename
  touches every import surface (backend, desktop service, lite, tests, docs).
  A partial revert reintroduces import breakage; roll back the whole commit or
  not at all.
- **Configuration authority moved to the database**: environment JSON values
  only seed empty tables. Rolling back code without the database is safe;
  rolling back the database is not.
- **Stateful delegates and Playwright browser tools are opt-in** (default
  off), so their risk is contained until
  [COV-23](https://linear.app/covalent-app/issue/COV-23/) stages the rollout.

## Merge Order and Target Version

- Target branch: `main` (completed). All later development branches from
  `main`, per policy §2.2.
- Versions planned for this train (see
  [RC.1 baseline](platform-0.2.0-rc.1.md)): shared packages `0.2.0rc1`,
  Enterprise `0.2.0-rc.1`, Desktop `0.1.0-beta.1`; Lite has no runnable
  release.
- Remaining gate before any RC tag: critical-path smoke tests
  ([COV-15](https://linear.app/covalent-app/issue/COV-15/)) and release review
  items in the baseline's Blocking Checks list.
