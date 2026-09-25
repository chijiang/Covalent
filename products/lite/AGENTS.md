# Lite development boundaries

- Read `docs/products/lite/development.md` and `contracts.md` before implementing features.
- CLI and API are thin entrypoints over the same framework-independent application use cases.
- Application modules must not import CLI, API, FastAPI, Typer, or read HTTP application state.
- Bootstrap owns adapter construction and cleanup. Use shared runtime/agent-kit; never import Enterprise or legacy `covalent`.
- V1 configuration is a validated immutable snapshot loaded from files; secrets use environment references. No DB, configuration write API, or Enterprise seed flow.
- V1 execution is request-scoped and stateless. Reject session persistence, background runs, approval/resume and replay instead of silently degrading.
- Do not declare CLI entrypoints or advertise runnable examples until implemented. Keep documentation status synchronized with code.
- Add dependencies when used. Check clean wheel installation before claiming isolation; agent-kit currently brings MCP and native execution transitively.
- Keep product tests under `products/lite/tests/`; shared boundary and cross-product tests belong at repository root.
