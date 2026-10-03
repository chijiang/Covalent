# covalent-runtime

The product-independent Covalent agent engine. Distribution: `covalent-runtime`;
import namespace: `covalent_runtime`.

[Repository overview](../../../README.md) · [Runtime consistency](../../../docs/runtime-consistency.md)

## Responsibilities

- ReAct execution and model/tool turn orchestration
- Context compaction and message sanitization
- Delegation and human-in-the-loop continuation semantics
- Durable run lifecycle services
- Domain types and ports for models, memory, execution and persistence

## Dependency boundary

Runtime may depend on `covalent-contracts` only. Registries, model SDKs,
databases, FastAPI, SQLAlchemy, Docker and product settings are injected through
ports and must not be imported, including under `TYPE_CHECKING`.

## Development

```bash
uv sync --package covalent-runtime
uv build --package covalent-runtime --wheel
uv run python -m pytest tests/architecture/test_monorepo.py
```

Use `tooling/verify_wheels.py` after building all workspace wheels to verify the
Runtime imports and executes without product or adapter packages installed.
