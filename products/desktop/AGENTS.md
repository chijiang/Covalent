# Desktop development boundaries

- Read docs/products/desktop/development.md, host-contract.md and docs/runtime-consistency.md before implementation.
- Stack direction: Electron shell, React/Vite renderer, Python sidecar. macOS and Windows are both release targets.
- Shell owns OS access and process lifecycle, never Agent execution logic. Renderer uses a narrow preload bridge; no Node integration or arbitrary IPC/command execution.
- Python dependency direction: api -> application -> runtime/contracts and injected ports. Application must not import FastAPI, api, or read app.state.
- Bootstrap owns concrete adapter composition. Do not import covalent_lite, covalent_enterprise or legacy covalent; do not copy their execution loops or configuration stores.
- Reuse React components through shared packages only; never import Enterprise source. Extract components with a real consumer and tests.
- Local persistence and migrations belong to Desktop infra initially. Do not create storage-local until genuine reuse warrants extraction.
- Templates contain definitions, dependency/capability requirements and secret references, not credentials, absolute host paths or session data.
- Keep scaffold/planned/implemented status explicit. Add executable entrypoints and launch instructions only with working implementations.
- Test packaged sidecars on both operating systems; local import checks do not establish desktop release readiness.
