# covalent_execution_docker

Docker execution adapter and process transport.

Allowed dependencies: runtime and execution-native runner resources, plus Docker SDK. No product imports.

Maintainer: Covalent runtime maintainers. Public imports live under `covalent_execution_docker`;
legacy `covalent.*` aliases live exclusively in the root compatibility distribution.
The package uses its own `pyproject.toml`; root uv workspace supplies local sources.

Validation: `uv run python -m pytest tests/` (existing regression suite),
`uv run python -m pytest tests/architecture/`, and `uv run python tooling/verify_wheels.py`
after building wheels. New package-specific tests can live in this package
and must be included in the root validation command.
