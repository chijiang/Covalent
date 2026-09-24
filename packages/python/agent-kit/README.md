# covalent_agent_kit

Standard registry, model adapters, MCP, Skill lifecycle and tools.

Allowed dependencies: contracts, runtime and execution-native. Document/browser capabilities are optional extras.

Maintainer: Covalent runtime maintainers. Public imports live under `covalent_agent_kit`;
legacy `covalent.*` aliases live exclusively in the root compatibility distribution.
The package uses its own `pyproject.toml`; root uv workspace supplies local sources.

Validation: `uv run python -m pytest tests/` (existing regression suite),
`uv run python -m pytest tests/architecture/`, and `uv run python tooling/verify_wheels.py`
after building wheels. New package-specific tests can live in this package
and must be included in the root validation command.
