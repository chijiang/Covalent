# covalent-agent-kit

Standard agent assembly components: registry, model adapters, MCP integration,
skill lifecycle and built-in tools. Distribution: `covalent-agent-kit`; import
namespace: `covalent_agent_kit`.

[Repository overview](../../../README.md) · [Skill design](../../../docs/skill-system-design.md)

## Responsibilities

- Agent, model, tool and skill registry
- OpenAI-compatible provider adapters
- MCP transports and tool normalization
- Skill discovery, process management and SDK resources
- Standard workspace, shell, browser and document tools

Document and browser capabilities are optional dependency extras.

## Dependency boundary

Agent Kit may depend on Contracts, Runtime and native runner resources. It must
not import products or concrete product storage. Product composition selects
providers, execution adapters and settings.

## Development

```bash
uv sync --package covalent-agent-kit
uv build --package covalent-agent-kit --wheel
uv run python -m pytest tests/architecture/test_monorepo.py
```

Import `covalent_agent_kit` directly; the removed `covalent.*` aliases are not
available.
