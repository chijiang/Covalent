# Covalent Lite

Lite is the small, CLI-first Covalent runtime for external AI-native
applications. It will expose a stable agent invocation surface without the
Enterprise control plane, PostgreSQL dependency or desktop shell.

**Status:** installable package scaffold. Agent assembly, CLI commands, config
loading and HTTP/SSE endpoints are not implemented yet.

[Repository overview](../../README.md) · [Development guide](../../docs/products/lite/development.md) · [API contracts](../../docs/products/lite/contracts.md)

## Intended scope

- File-based agent, provider, MCP and skill configuration
- CLI-first validation, listing and invocation
- Small HTTP API with synchronous and SSE agent calls
- Native execution by default, with explicit optional Docker support
- The same contracts, Runtime and Agent Kit semantics as Enterprise and Desktop

Lite does not own user administration, workspaces, audit dashboards, database
configuration, enterprise policy or a permanent frontend.

## Structure

```text
products/lite/
└── service/
    └── src/covalent_lite/
        ├── api/          # Future HTTP transport
        ├── application/  # Lite use cases and composition
        ├── cli/          # Future user-facing CLI
        └── config/       # File configuration loading and validation
```

The distribution is `covalent-lite`; its Python namespace is `covalent_lite`.

## Development

Run from the repository root:

```bash
uv sync --package covalent-lite
uv run --package covalent-lite python -c "import covalent_lite"
```

This verifies the current package scaffold only. Enterprise remains the default
root environment; run `uv sync` to restore it after package-only work.

## Validation

```bash
uv run python -m pytest tests/architecture/test_monorepo.py
uv build --package covalent-lite --wheel
```

## Boundaries

- Depend on shared packages, never on Enterprise or Desktop.
- Keep runtime behavior in `covalent_runtime`; Lite owns assembly and transport.
- Keep configuration portable and filesystem-based.
- Preserve the common invocation and SSE contracts documented for Lite.
