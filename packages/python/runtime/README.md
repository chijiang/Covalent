# covalent_runtime

ReAct, context compaction, delegation, durable runs, execution types and ports.

Allowed dependencies: contracts only. Concrete registries, models, stores and execution backends are injected.

Maintainer: Covalent runtime maintainers. Public imports live under `covalent_runtime`;
The removed `covalent.*` aliases are not supported; import `covalent_runtime` directly.
The package uses its own `pyproject.toml`; root uv workspace supplies local sources.

Validation: `uv run python -m pytest tests/` (existing regression suite),
`uv run python -m pytest tests/architecture/`, and `uv run python tooling/verify_wheels.py`
after building wheels. New package-specific tests can live in this package
and must be included in the root validation command.
