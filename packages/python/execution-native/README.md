# covalent_execution_native

Native execution adapter and packaged Python/Node Skill runners.

Allowed dependencies: runtime only. Settings are supplied by the host. Native subprocesses are not an OS security sandbox.

Maintainer: Covalent runtime maintainers. Public imports live under `covalent_execution_native`;
legacy `covalent.*` aliases live exclusively in the root compatibility distribution.
The package uses its own `pyproject.toml`; root uv workspace supplies local sources.

Validation: `uv run python -m pytest tests/` (existing regression suite),
`uv run python -m pytest tests/architecture/`, and `uv run python tooling/verify_wheels.py`
after building wheels. New package-specific tests can live in this package
and must be included in the root validation command.
