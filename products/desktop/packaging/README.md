# Desktop Release Acceptance

Cross-platform packaging, signing, installation and upgrade acceptance criteria for Desktop.

**Current status: macOS local packaging is implemented (ad-hoc signed prototype, see below); Windows packaging, production signing, notarization and installers remain outstanding.** The first release covers macOS and Windows; validate macOS arm64/x64 and Windows x64, while native Windows arm64 support is declared only after separate validation.
The minimum supported OS version is recorded once the Electron and Python packaging tool versions are chosen; it must not be declared from a development machine alone.

[Desktop README](../README.md) · [Development guide](../../../docs/products/desktop/development.md)

## Build pipeline goals

1. Build contracts/runtime/agent-kit/Desktop wheels per OS/architecture, and install them at pinned versions into an isolated environment.
2. Generate a standalone sidecar with the selected Python packaging solution; the prototype prefers PyInstaller, freezing dependencies and resource hooks.
3. Build the React static assets and Electron main/preload, and combine them into a single Desktop distributable.
4. Perform code signing, macOS notarization and Windows installer verification; signing credentials are provided by CI secrets.
5. Produce a release manifest: product version, commit, core package versions, template/API protocol versions, OS/architecture, asset digests.
6. Release after install/upgrade/cleanup testing; upgrades replace the UI, shell and sidecar as a whole and must never drift the kernel independently.

## macOS local packaging (implemented prototype)

Run from the repository root:

```bash
pnpm package:desktop:mac
```

Artifacts land in `products/desktop/shell/build/`:

- `Covalent Desktop.app`: a self-contained application (Electron/Chromium, the frozen sidecar and the renderer all live inside the bundle), ad-hoc signed and directly runnable.
- `Covalent-Desktop-<version>-macos-<arch>.dmg`: a zlib-compressed image (UDZO) of the same .app, containing the .app plus an `/Applications` symlink for drag-install.

Pipeline (`shell/scripts/package-mac.mjs` orchestrates; `shell/scripts/package-service.mjs` freezes the sidecar):

1. `pnpm --filter @covalent/desktop-shell build` builds the renderer (Vite) and the shell (tsc).
2. PyInstaller onedir freezes `covalent_desktop` into `covalent-desktop-service`. Key flags: `--copy-metadata mcp` (`mcp/client.py` reads `importlib.metadata.version` at runtime), `--collect-data covalent_agent_kit` and `--collect-data covalent_execution_native` (carry `skill_sdk.js`, `node_runner.js` and other data files), and `--collect-all playwright`. The freeze self-test starts the binary with `COVALENT_DESKTOP_SERVICE_TOKEN` set and validates the single-line ready JSON on stdout.
3. Assemble the bundle from `node_modules/electron/dist/Electron.app` (copied with `ditto` to preserve symlinks). The layout mirrors the shell's packaged path conventions: `Contents/Resources/app/` (shell package.json + dist), `Contents/Resources/web/dist/` (renderer), and `Contents/Resources/service/` (frozen sidecar flattened so the binary sits exactly at `Resources/service/covalent-desktop-service`).
4. **`Contents/MacOS/Electron` must be renamed and `CFBundleExecutable` updated accordingly**: `app.isPackaged` compares the first 8 characters of the executable name against "electron" (case-insensitive), so without the rename a packaged instance takes the dev branch and looks for `.venv/bin/python`.
5. PlistBuddy writes `CFBundleName`/`CFBundleDisplayName`/`CFBundleIdentifier` (com.covalent.desktop); an icns is generated from `covalent-mark.png` via `sips` + `iconutil`; `codesign --force --deep -s -` re-signs ad hoc (unsigned binaries cannot run on arm64); `hdiutil create -format UDZO` produces the dmg.

Current prototype boundaries:

- Ad-hoc signing: the app runs directly when the file carries no quarantine mark (internal or hand-copied distribution); public distribution requires a Developer ID signature (hardened runtime + entitlements) plus notarization and stapling, per pipeline goal 4.
- arm64 only; x64 must be built on the matching architecture.
- Playwright browser binaries are not bundled; the runtime shares `~/Library/Caches/ms-playwright`, and browser tools degrade through the existing error path when they are missing.
- Upgrade/uninstall semantics (data migration for `~/Library/Application Support/Covalent Desktop`, orphan process reclamation) still follow "must validate on every platform"; the prototype does not cover them.

## Must validate on every platform

- Launch in environments without system Python or development tools; run from paths containing spaces or non-ASCII characters.
- Bundled Python/Node runner resources resolve correctly; missing optional external runtimes fail with clear errors.
- Single instance, sidecar handshake, failure diagnostics and service version incompatibility handling.
- SSE output, tool runs, user cancellation, window close/app quit, and no orphan processes after abnormal exits.
- Reclaim the Windows process tree and macOS process groups separately; never stop at the top-level Python PID.
- Credential storage, local directory grants, log redaction and app data directory permissions.
- A failed database migration keeps old data recoverable; a failed auto-update can still start a compatible version.

Session persistence is not crash recovery; leftover executions from a first release are marked interrupted and side-effectful tools are never replayed automatically.
Check schema compatibility before rollback; an upgraded database must never be handed back to an incompatible older build.
The Python freeze approach is validated in the macOS prototype (PyInstaller onedir); the auto-update vendor, signing identity and final installer format remain to be locked.
