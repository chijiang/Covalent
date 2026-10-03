# 分支、版本与发布规范

状态：项目开发规范。除文中明确标注为“过渡期”的内容外，新开发和发布必须遵循本文。

本规范适用于 Covalent monorepo 中的共享 Python 包、Enterprise、Desktop、Lite 和后续 Monitor 产品。架构与产品边界以[架构设计](monorepo-architecture.md)为准；本文只规定代码如何进入主干、如何生成 Preview，以及如何形成可追溯的 Release。

## 1. 基本原则

1. `main` 是唯一永久开发分支，并且始终保持可发布；不再维护长期 `dev`、`develop` 或 `pre-release` 分支。
2. 所有变更从最新 `main` 创建短期分支，通过 Pull Request 合并；禁止直接向 `main` 推送。
3. 一个提交对应一组不可变制品。Preview、RC 和 Stable 应晋升同一提交产生的制品，不在发布阶段重新构建不同内容。
4. 共享 Python 包初期使用同一版本列车；Enterprise、Desktop、Lite 和 Monitor 使用独立产品版本、Tag、Changelog 与发布流程。
5. 产品版本、公共协议版本和数据库迁移版本彼此独立。升级产品版本不得替代协议兼容检查或数据库迁移。
6. Release Tag 只指向通过全部发布门禁的提交；Tag 一经发布不得移动、覆盖或复用。

## 2. 分支模型

### 2.1 永久分支

| 分支 | 用途 | 规则 |
| --- | --- | --- |
| `main` | 唯一主干和下一版本集成线 | 受保护；只接受 PR；不得 force push；始终可部署到 staging |

`main` 不等于生产环境。生产版本由带签名的 Release Tag 和对应不可变制品标识。

### 2.2 短期分支

| 类型 | 命名格式 | 基线 | 合并目标 |
| --- | --- | --- | --- |
| 功能 | `feat/<issue>-<slug>` | 最新 `main` | `main` |
| 修复 | `fix/<issue>-<slug>` | 最新 `main` | `main` |
| 文档 | `docs/<issue>-<slug>` | 最新 `main` | `main` |
| 工程维护 | `chore/<issue>-<slug>` | 最新 `main` | `main` |
| 发布稳定 | `release/<scope>/<version>` | 准备发布的 `main` 提交 | `main` |
| 生产热修复 | `hotfix/<scope>/<version>` | 对应产品最近的 Stable Tag | `main` |

`<issue>` 优先使用 Linear 编号，例如 `feat/COV-123-session-search`。没有 Issue 的微小文档或维护变更可以省略编号，但必须保留可读的 `<slug>`。

开发分支应在 PR 合并后自动删除。`release/*` 仅用于需要跨多天稳定、签名或候选验证的发布，不作为下一批功能的集成分支。无需稳定窗口的产品可以直接从绿色 `main` 提交打 Tag。

### 2.3 合并策略

- 常规 PR 使用 **Squash merge**，使每个 PR 在 `main` 上形成一个可回滚提交。
- PR 标题使用 Conventional Commits 风格：`feat: ...`、`fix: ...`、`docs: ...`、`chore: ...`、`refactor: ...`。
- 禁止将未同步最新 `main`、存在未解决讨论或必需检查失败的 PR 合并。
- 历史仓库收口、大规模外部代码导入等需要保留拓扑的特殊操作，可以经维护者确认后使用 merge commit；不得将该例外用于日常功能开发。
- 不使用 rebase 或 force push 改写已经发布 Tag 可达的历史。

### 2.4 `main` 保护规则

GitHub 必须为 `main` 开启以下保护：

1. 必须通过 Pull Request 合并，并解决全部 review conversation。
2. 至少一名维护者批准；公共协议变更还需一个受影响产品的维护者批准。
3. 必需检查包括 Enterprise backend、Enterprise web、容器验证和 Desktop macOS/Windows；按路径缩小检查前，影响分析必须能够覆盖反向依赖。
4. 禁止 force push 和分支删除；管理员仅可在生产事故处置中临时绕过，并留下审计记录。
5. 合并后自动删除源分支。

## 3. 版本模型

### 3.1 版本对象

| 对象 | 版本策略 | Tag 示例 |
| --- | --- | --- |
| 共享 Python 包 | 当前使用统一版本列车 | `packages/v0.2.0` |
| Enterprise | 独立 SemVer | `enterprise/v0.2.0` |
| Desktop | 独立 SemVer | `desktop/v0.1.0` |
| Lite | 独立 SemVer | `lite/v0.1.0` |
| Monitor | 独立 SemVer | `monitor/v0.1.0` |
| Agent/事件/控制协议 | 独立 `schema_version` | 不以产品 Tag 代替 |

当前共享 Python 包使用精确的内部版本约束，因此一次共享包变更必须同步更新所有受影响包的 `pyproject.toml`、产品依赖和根 `uv.lock`。在依赖范围与自动兼容测试成熟前，不允许单独发布其中一个共享包。

前端 workspace 包当前为私有包。其版本只在成为独立发布制品后纳入公开版本管理；产品构建仍必须固定 `pnpm-lock.yaml`。

### 3.2 SemVer 规则

在 `0.x` 阶段采用以下约定：

- `0.MINOR.0`：新增能力、破坏性产品变更或需要显式迁移的变更。
- `0.MINOR.PATCH`：向后兼容的缺陷、安全和文档修复。
- `1.0.0` 以后严格遵循 SemVer：破坏性变更升 Major，兼容功能升 Minor，兼容修复升 Patch。

公共协议已经有稳定消费者后，破坏性 schema 变更必须提升协议 Major；新增可选字段通常保持兼容。数据库 revision 继续由 Alembic 或对应产品迁移系统管理，不使用产品版本号作为 revision。

### 3.3 预发布版本

发布通道依次为：

```text
alpha → beta → rc → stable
```

- `alpha`：功能和结构可能继续变化，仅供开发验证。
- `beta`：主要功能完整，允许修复和小范围兼容调整。
- `rc`：发布候选，只接受阻塞发布的修复。
- `stable`：完成发布门禁并面向目标用户。

Git Tag 使用 `enterprise/v0.2.0-rc.1` 形式。Python 包使用 PEP 440 版本，例如 `0.2.0rc1`；NPM/产品版本使用 `0.2.0-rc.1`。同一制品在不同系统中的格式可以不同，但 Release Manifest 必须明确它们属于同一发布。

## 4. Preview 规范

### 4.1 Pull Request Preview

每个 PR 至少完成受影响范围的构建和测试。修改 contracts、runtime、根锁文件或影响图无法判断时，运行完整产品矩阵。

制品必须包含 PR 编号与完整或短 Commit SHA，例如：

```text
Python:   0.2.0.dev123+g1a2b3c4
Docker:   ghcr.io/<owner>/<image>:pr-123-1a2b3c4
Desktop:  Covalent-Desktop-pr123-1a2b3c4-<os>-<arch>
```

PR Preview 规则：

1. Enterprise 为需要端到端验证的 PR 创建隔离环境、数据库与凭据，地址使用 `pr-<number>` 标识。
2. 数据库至少验证从空库迁移；涉及迁移时还需验证从最近 Stable 版本升级。
3. Desktop 生成未签名或 ad-hoc 签名的 macOS/Windows 测试制品并运行安装、启动、sidecar 握手和清理 smoke test。
4. Lite 构建 wheel 并在干净环境运行导入、CLI 和启动 smoke test；在产品实现完成前至少保留包隔离验证。
5. Preview 不使用生产密钥或生产数据；PR 关闭后自动销毁环境，普通制品保留 7 至 14 天。
6. CI 在 PR 中回写 Preview 地址、制品、测试结果与已知限制。

来自 fork 的不可信 PR 不注入 Secret，也不自动部署到有内部网络权限的环境。

### 4.2 主干 Preview

每个绿色 `main` 提交生成 `main-<sha>` 制品并持续部署到 staging。可以提供方便人工验证的 `edge` 浮动别名，但所有问题记录和发布晋升必须引用不可变 SHA 或 digest。

夜间验证补充耗时较长的完整容器矩阵、真实数据库升级、Desktop 双平台打包、安装升级和异常进程清理。夜间失败必须进入待办，但不得把已知失败的 `main` 制品晋升为 RC。

## 5. Release 流程

### 5.1 准备发布

1. 确定发布产品、目标版本和候选 Commit SHA。
2. 更新相应版本源、Changelog 与 Release Manifest；共享包变更按统一版本列车更新所有精确依赖。
3. 若需要稳定窗口，从候选提交创建 `release/<scope>/<version>`；冻结新功能，只接受发布阻塞修复。
4. 运行完整发布门禁，生成 `rc.N` Tag 和不可变候选制品。
5. 在目标环境验证数据库升级、配置兼容、容器健康和 Desktop 安装/升级/卸载。

### 5.2 正式发布

1. 从已验证 RC 对应的同一 Commit 和制品 digest 创建 Stable Tag；禁止重新编译内容不同的“同版本”制品。
2. 校验 Tag 中的 scope/version 与产品 manifest 完全一致。
3. 生成 SBOM、checksum、Release Notes，并发布对应 wheel、镜像或安装包。
4. Docker 可以在不可变 digest 上增加 `v0.2.0`、`v0.2` 和 `latest` 别名；`latest` 不能作为部署清单中的唯一版本依据。
5. 完成最小生产 smoke test，记录发布结果；将 release 分支中的必要修复合回 `main` 后删除该分支。

### 5.3 Release Manifest

每个产品发布必须记录：

- 产品名、产品版本、Release Tag 和 Git Commit SHA；
- 所有共享包的名称与精确版本；
- 支持的公共协议/schema 版本范围；
- 数据库 migration head 或本地存储 schema 版本；
- 构建制品、目标 OS/架构、镜像 digest 和文件 checksum；
- 构建工作流运行编号、签名/公证状态和发布日期。

Manifest 必须随 Release 制品保存，并能从 Release 页面直接取得。不得包含访问密钥或其他凭据。

## 6. Hotfix 流程

1. 从受影响产品最近的 Stable Tag 创建 `hotfix/<scope>/<version>`，不能从当前未发布的 `main` 创建。
2. 只提交解决生产问题所需的最小改动和回归测试。
3. 运行该产品完整发布门禁以及受影响共享包的反向依赖测试。
4. 发布 Patch 版本后，通过 PR 将修复合回 `main`；如仍有活动 release 分支，也同步合入。
5. 删除 hotfix 分支，但保留不可变 Tag、Manifest 和制品。

## 7. Changelog 与发布说明

- 产品分别维护 Changelog；共享包维护一个 release-train Changelog。
- Changelog 只记录用户或集成方可感知的功能、修复、破坏性变更、安全事项和迁移要求，不复制全部 Git commit。
- PR 必须标注影响的产品/包，并在存在破坏性变更时说明迁移方式。
- Release Notes 包含 Highlights、Breaking Changes、Migration、Known Issues、Checksums 和从上一 Stable Tag 起的完整变更链接。

## 8. 当前仓库的过渡规则

首次按本规范收口时，从现有完整集成提交建立 `release/platform/0.2.0`，通过一个保留历史的 recovery PR 合入 `main`。该 PR 是日常 squash merge 规则的明确例外。

旧的 `dev`、`felines`、`multi-line-prod`、`multi-user-support`、`sandbox-backend`、`wasm-sandbox` 在确认提交均可从新 release 分支到达后删除。仍有独有提交的旧分支先重命名为 `archive/<name>-<purpose>`；只有在内容被迁移或明确废弃后才删除。

过渡完成的判定条件：

1. recovery PR 的必需 CI 全绿并合入 `main`；
2. `main` 保护规则生效；
3. 至少一个产品的 scoped RC Tag 能完整执行构建与发布工作流；
4. 后续开发均从 `main` 创建符合命名规范的短期分支。

