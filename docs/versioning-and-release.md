# Branching, Versioning, and Release Policy

Status: project development standard. All new development and releases must follow this policy except where a transition rule is explicitly stated.

This policy applies to the shared Python packages and the Enterprise, Desktop, Lite, and future Monitor products in the Covalent monorepo. See the [architecture document](monorepo-architecture.md) for product and package boundaries. This document defines how code enters the mainline, how Preview artifacts are produced, and how a traceable Release is created.

## 1. Principles

1. `main` is the only permanent development branch and must remain releasable. Do not create long-lived `dev`, `develop`, or `pre-release` branches.
2. Create every change from the latest `main` and merge it through a Pull Request. Direct pushes to `main` are prohibited.
3. One commit identifies one immutable set of artifacts. Promote the same artifacts through Preview, RC, and Stable instead of rebuilding different content during release.
4. Shared Python packages initially use one release train. Enterprise, Desktop, Lite, and Monitor use independent product versions, Tags, Changelogs, and release workflows.
5. Product versions, public protocol versions, and database migration versions are independent. A product version bump does not replace compatibility checks or database migrations.
6. A Release Tag may point only to a commit that passed every release gate. Never move, overwrite, or reuse a published Tag.

## 2. Branch Model

### 2.1 Permanent Branch

| Branch | Purpose | Rules |
| --- | --- | --- |
| `main` | The only mainline and integration branch for the next release | Protected; Pull Requests only; no force pushes; continuously deployable to staging |

`main` does not represent production. A production version is identified by a signed Release Tag and its immutable artifacts.

### 2.2 Short-Lived Branches

| Type | Naming pattern | Base | Merge target |
| --- | --- | --- | --- |
| Feature | `feat/<issue>-<slug>` | Latest `main` | `main` |
| Fix | `fix/<issue>-<slug>` | Latest `main` | `main` |
| Documentation | `docs/<issue>-<slug>` | Latest `main` | `main` |
| Maintenance | `chore/<issue>-<slug>` | Latest `main` | `main` |
| Release stabilization | `release/<scope>/<version>` | The `main` commit selected for release | `main` |
| Production hotfix | `hotfix/<scope>/<version>` | The affected product's latest Stable Tag | `main` |

Use a Linear identifier for `<issue>` when one exists, for example `feat/COV-123-session-search`. A small documentation or maintenance change without an Issue may omit the identifier but must retain a descriptive `<slug>`.

Delete development branches automatically after their PR is merged. Use `release/*` only when signing, stabilization, or release-candidate verification spans multiple days. It is not an integration branch for the next batch of features. A product that does not need a stabilization window may be tagged directly from a green `main` commit.

### 2.3 Merge Policy

- Use **Squash merge** for normal Pull Requests so each PR becomes one revertible commit on `main`.
- Use Conventional Commits style for PR titles: `feat: ...`, `fix: ...`, `docs: ...`, `chore: ...`, or `refactor: ...`.
- Do not merge a PR that is behind the required `main` baseline, has unresolved conversations, or has failed required checks.
- A repository-history consolidation or a large external import may use a merge commit with maintainer approval when preserving topology is necessary. This exception does not apply to normal feature work.
- Do not rebase or force-push history reachable from a published Tag.

### 2.4 `main` Protection

Configure GitHub protection for `main` as follows:

1. Require a Pull Request and require all review conversations to be resolved.
2. Require at least one maintainer approval. A public-protocol change also requires approval from a maintainer of an affected product.
3. Require Enterprise backend, Enterprise web, container, and Desktop macOS/Windows checks. Path-based check reduction is allowed only after dependency-impact analysis covers reverse dependencies.
4. Prohibit force pushes and branch deletion. Administrators may bypass protection only to resolve a production incident and must leave an audit record.
5. Delete the source branch automatically after merge.

For a repository with only one maintainer, enforce the PR and status-check requirements immediately and enable mandatory external approval as soon as another maintainer is available.

## 3. Version Model

### 3.1 Versioned Objects

| Object | Version strategy | Tag example |
| --- | --- | --- |
| Shared Python packages | One release train during the initial phase | `packages/v0.2.0` |
| Enterprise | Independent SemVer | `enterprise/v0.2.0` |
| Desktop | Independent SemVer | `desktop/v0.1.0` |
| Lite | Independent SemVer | `lite/v0.1.0` |
| Monitor | Independent SemVer | `monitor/v0.1.0` |
| Agent, event, and control protocols | Independent `schema_version` | Never replaced by a product Tag |

The shared Python packages currently use exact internal version constraints. A shared-package change must therefore update every affected `pyproject.toml`, all consuming product dependencies, and the root `uv.lock` together. Do not release only one shared package until dependency ranges and automated compatibility tests are mature.

Frontend workspace packages are currently private. They enter public version management only when they become independently published artifacts. Product builds must still pin `pnpm-lock.yaml`.

### 3.2 SemVer Rules

During the `0.x` phase:

- `0.MINOR.0` introduces a capability, a breaking product change, or a change that requires an explicit migration.
- `0.MINOR.PATCH` delivers backward-compatible fixes, security changes, or documentation corrections.
- Starting with `1.0.0`, follow SemVer strictly: breaking changes increment Major, compatible features increment Minor, and compatible fixes increment Patch.

After a public protocol has stable consumers, a breaking schema change must increment the protocol Major version. Adding optional fields is normally backward-compatible. Database revisions remain owned by Alembic or the corresponding product migration system; a product version is not a database revision.

### 3.3 Prerelease Versions

Release channels progress in this order:

```text
alpha -> beta -> rc -> stable
```

- `alpha`: structure and functionality may continue to change; intended for development validation.
- `beta`: primary functionality is complete; fixes and limited compatibility adjustments are allowed.
- `rc`: release candidate; accept only release-blocking fixes.
- `stable`: all release gates passed and the release is ready for its target audience.

Use a Git Tag such as `enterprise/v0.2.0-rc.1`. Use PEP 440 for Python packages, such as `0.2.0rc1`, and `0.2.0-rc.1` for NPM and product versions. Formats may differ between ecosystems, but the Release Manifest must map them to the same release.

## 4. Preview Policy

### 4.1 Pull Request Preview

Every PR must build and test its affected dependency closure. Changes to contracts, runtime, root lockfiles, or inputs that cannot be classified by the dependency graph require the full product matrix.

Artifact identifiers must include the PR number and a full or short Commit SHA, for example:

```text
Python:   0.2.0.dev123+g1a2b3c4
Docker:   ghcr.io/<owner>/<image>:pr-123-1a2b3c4
Desktop:  Covalent-Desktop-pr123-1a2b3c4-<os>-<arch>
```

PR Preview requirements:

1. Create an isolated environment, database, and credentials for Enterprise PRs that need end-to-end validation. Include `pr-<number>` in the environment address.
2. Test database migration from an empty database. When a migration changes, also test an upgrade from the latest Stable release.
3. Produce unsigned or ad-hoc-signed macOS and Windows Desktop artifacts and run installation, startup, sidecar handshake, and cleanup smoke tests.
4. Build the Lite wheel and run import, CLI, and startup smoke tests in a clean environment. Preserve at least package-isolation validation until the Lite product implementation is complete.
5. Never use production secrets or production data. Destroy an environment when its PR closes and retain ordinary Preview artifacts for 7 to 14 days.
6. Post the Preview URL, artifacts, test results, and known limitations back to the PR.

Do not expose Secrets to untrusted PRs from forks or deploy them into an environment with access to internal networks.

### 4.2 Mainline Preview

Every green `main` commit produces `main-<sha>` artifacts and is continuously deployable to staging. An `edge` floating alias may be provided for manual testing, but incidents and release promotions must reference an immutable SHA or digest.

Nightly validation supplements the PR gates with expensive container matrices, real database upgrades, two-platform Desktop packaging, installation upgrades, and abnormal-process cleanup. A failed nightly run must become tracked work, and an artifact with a known failure must not be promoted to RC.

## 5. Release Process

### 5.1 Prepare a Release

1. Select the product, target version, and candidate Commit SHA.
2. Update the version source, Changelog, and Release Manifest. For a shared-package change, update the entire package release train and all exact dependencies.
3. If a stabilization window is needed, create `release/<scope>/<version>` from the candidate commit. Freeze features and accept only release-blocking fixes.
4. Run every release gate and produce an `rc.N` Tag with immutable candidate artifacts.
5. Validate database upgrades, configuration compatibility, container health, and Desktop installation, upgrade, and removal in target environments.

### 5.2 Publish a Stable Release

1. Create the Stable Tag from the same Commit and artifact digest as the verified RC. Do not rebuild different content under the same version.
2. Verify that the scope and version in the Tag exactly match the product manifest.
3. Generate an SBOM, checksums, and Release Notes, then publish the corresponding wheels, images, or installers.
4. Docker may add `v0.2.0`, `v0.2`, and `latest` aliases to an immutable digest. Deployment manifests must not rely on `latest` as their only version identifier.
5. Run a minimal production smoke test and record the result. Merge required release-branch fixes back into `main`, then delete the release branch.

### 5.3 Release Manifest

Every product release records:

- product name, product version, Release Tag, and Git Commit SHA;
- exact name and version of every shared package;
- supported public protocol and schema version ranges;
- database migration head or local-storage schema version;
- artifacts, target OS and architecture, image digests, and file checksums;
- build workflow run identifier, signing and notarization state, and publication date.

Store the Manifest with the Release artifacts and make it directly downloadable from the Release page. It must not contain credentials or other secrets.

## 6. Hotfix Process

1. Create `hotfix/<scope>/<version>` from the affected product's latest Stable Tag, not from unreleased `main`.
2. Commit only the minimal production fix and its regression tests.
3. Run the product's complete release gates and reverse-dependency tests for any affected shared package.
4. Publish a Patch version, then merge the fix back to `main` through a PR. Apply it to any active release branch as well.
5. Delete the hotfix branch, but retain the immutable Tag, Manifest, and artifacts.

## 7. Changelog and Release Notes

- Maintain a separate Changelog for each product and one release-train Changelog for the shared packages.
- Record user-visible or integration-visible features, fixes, breaking changes, security notes, and migration requirements. Do not copy the full Git commit list.
- Every PR must identify the affected products and packages. A breaking change must include migration instructions.
- Release Notes contain Highlights, Breaking Changes, Migration, Known Issues, Checksums, and a complete comparison link from the previous Stable Tag.

## 8. Repository Transition

For the initial adoption of this policy, create `release/platform/0.2.0` from the existing integrated history and merge it into `main` through a recovery PR that preserves history. This PR is the documented exception to the normal squash-merge rule.

Delete the old `dev`, `felines`, `multi-line-prod`, `multi-user-support`, `sandbox-backend`, and `wasm-sandbox` branches after verifying that their commits are reachable from the new release branch. Rename an old branch with unique commits to `archive/<name>-<purpose>` and delete it only after its content is migrated or explicitly retired.

The transition is complete when:

1. all required checks on the recovery PR pass and it is merged into `main`;
2. `main` protection is active;
3. at least one product can run the complete build and release workflow from a scoped RC Tag; and
4. all subsequent development begins from `main` on a compliant short-lived branch.

