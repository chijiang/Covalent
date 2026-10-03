# covalent-contracts

Serializable cross-product contracts for agents, providers, MCP servers, skills
and messages. Distribution: `covalent-contracts`; import namespace:
`covalent_contracts`.

[Repository overview](../../../README.md) · [Architecture](../../../docs/monorepo-architecture.md)

## Responsibilities

- Pydantic configuration models exchanged by products
- Wire-safe message and content representations
- Stable identifiers and enums required across process or product boundaries

## Dependency boundary

Contracts may depend on Pydantic only. They must not import Runtime, adapters or
products. Keep behavior and infrastructure out of serializable schemas.

## Development

```bash
uv sync --package covalent-contracts
uv build --package covalent-contracts --wheel
uv run python -m pytest tests/architecture/test_monorepo.py
```

The removed `covalent.*` aliases are unsupported; import
`covalent_contracts` directly.
