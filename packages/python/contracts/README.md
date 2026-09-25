# covalent_contracts

Agent/Provider/MCP/Skill configuration and serializable messages.

Allowed dependencies: Pydantic only; no Runtime, products or infrastructure.

Maintainer: Covalent runtime maintainers. Public imports live under `covalent_contracts`;
The removed `covalent.*` aliases are not supported; import `covalent_contracts` directly.
The package uses its own `pyproject.toml`; root uv workspace supplies local sources.

Validation: `uv run python -m pytest tests/` (existing regression suite),
`uv run python -m pytest tests/architecture/`, and `uv run python tooling/verify_wheels.py`
after building wheels. New package-specific tests can live in this package
and must be included in the root validation command.
