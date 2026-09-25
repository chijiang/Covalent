# Legacy Python import retirement

The root `agent-framework` distribution and its `covalent.*` compatibility
namespace were removed on 2026-09-25. The repository root is now a non-package
uv workspace, so every implementation has one owning package and one canonical
import path.

Use these replacements when migrating external callers:

| Removed namespace | Canonical owner |
| --- | --- |
| `covalent.core.types`, `covalent.core.agent` | `covalent_contracts` and `covalent_runtime.domain` |
| `covalent.runtime` | `covalent_runtime` |
| `covalent.runtime.backend` | `covalent_runtime.ports.execution`; Enterprise composition is `covalent_enterprise.infra.execution` |
| `covalent.registry`, `covalent.model`, `covalent.mcp`, `covalent.skills`, `covalent.core.*_tools` | `covalent_agent_kit` |
| `covalent.runtime.filesystem_backend` | `covalent_execution_native` |
| `covalent.runtime.docker_backend` | `covalent_execution_docker` |
| `covalent.api`, `covalent.application`, `covalent.infra`, `covalent.cli` | `covalent_enterprise` |

There is no fallback alias. Install the package that owns the API and import it
directly. Architecture tests reject new imports from the removed namespace, and
wheel verification confirms that clean installations do not expose `covalent`.
