# covalent-execution-native

Native subprocess execution adapter and packaged Python/Node skill runners.
Distribution: `covalent-execution-native`; import namespace:
`covalent_execution_native`.

[Repository overview](../../../README.md) · [Execution design](../../../docs/execution-backend-design.md)

## Responsibilities

- Execute commands and skill processes on the host
- Manage process I/O, timeouts and termination
- Package runner resources used by native and Docker execution
- Map Runtime execution ports to local filesystem/process behavior

## Dependency boundary

This package may depend on Runtime. Host settings are supplied by the product.
It must not import products or Docker-specific implementations. Native execution
is a process boundary, not an operating-system security sandbox.

## Development

```bash
uv sync --package covalent-execution-native
uv build --package covalent-execution-native --wheel
uv run python -m pytest tests/architecture/test_monorepo.py
```
