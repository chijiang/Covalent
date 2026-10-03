# covalent-execution-docker

Docker execution adapter for isolated agent skill and script processes.
Distribution: `covalent-execution-docker`; import namespace:
`covalent_execution_docker`.

[Repository overview](../../../README.md) · [Sandbox images](../../../sandbox/README.md) · [Execution design](../../../docs/execution-backend-design.md)

## Responsibilities

- Create and supervise per-agent session containers
- Execute runner processes over Docker's multiplexed exec transport
- Apply resource limits, mounts and network policy
- Reclaim idle, reset and stateless-run containers

## Dependency boundary

This package may depend on Runtime, native runner resources and the Docker SDK.
It must not import product configuration or persistence. Products translate
their settings and stored sandbox profiles into Runtime execution contracts.

## Development

```bash
uv sync --package covalent-execution-docker
uv build --package covalent-execution-docker --wheel
uv run python -m pytest tests/test_docker_backend.py
```

Real integration cases require a reachable Docker daemon and sandbox images;
unit tests use fake clients and run without Docker.
