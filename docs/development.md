# Monorepo 开发导航

## 当前落地状态

| 区域 | 状态 | 入口与职责 |
| --- | --- | --- |
| `products/enterprise/backend` | 已有实现 | 企业 API、用例、数据库、CLI 和迁移 |
| `products/enterprise/web` | 已有实现 | Next.js 控制台 |
| `products/lite/service` | 包骨架 | 独立 Lite 命名空间；CLI/API 待实现 |
| Desktop、Monitor | 规划 | 暂不建立空应用或引入宿主、数据库依赖 |
| `packages/python/contracts` | 已有实现 | 可序列化消息和 Agent/Provider/MCP/Skill 配置 |
| `packages/python/runtime` | 已有实现 | 执行领域、端口、引擎和运行服务 |
| `packages/python/agent-kit` | 已有实现 | 模型、MCP、Skill、工具和 registry 实现 |
| `packages/python/execution-*` | 已有实现 | native / Docker 执行适配器 |
| `src/covalent` | 兼容层 | 只保留旧导入，不添加实现 |

完整目标见 [架构设计](monorepo-architecture.md)，已完成的搬迁与验证见 [迁移记录](monorepo-migration.md)。
目标目录不代表功能已经交付。共享包优先服务真实消费者，不预建所有规划包。

## 从哪里开始

- Enterprise：阅读 [产品说明](../products/enterprise/README.md)，沿现有 API → application → runtime/infra 修改。
- Lite：阅读 [开发指南](products/lite/development.md) 和 [首版接口约定](products/lite/contracts.md)。
- 共享执行行为：修改 runtime 的引擎/端口；具体模型与工具实现归 agent-kit。
- 公共数据结构：修改 contracts；企业管理 DTO 保留在 Enterprise。
- Monitor：独立观测消费者，未来通过版本化协议接入；不得直接读运行产品的业务库。

产品禁止互相导入，共享包禁止导入产品；根 `tests/architecture/test_monorepo.py` 检查 Python 边界。
CLI/API 用例不重复实现，基础设施在产品装配入口注入。

## 开发与验证

在仓库根执行，Python 3.12+；Node/pnpm 版本遵循根 `package.json`。

```sh
uv sync --locked
uv run python main.py serve --port 5170
# 需要数据库迁移时显式执行：uv run python main.py migrate
pnpm install --frozen-lockfile
pnpm dev:enterprise
```

后端改动先跑受影响测试，再跑依赖边界与 lint：

```sh
uv run python -m pytest tests/architecture/test_monorepo.py
uv run ruff check --select F packages/python products/enterprise/backend/src products/lite/service/src src main.py
```

前端改动运行 `pnpm typecheck` 和 `pnpm lint`。涉及数据库、发行物或共享执行行为时，增加对应迁移、独立 wheel 安装或消费者测试。
Lite 产品测试实施后单独运行 `uv run --package covalent-lite python -m pytest products/lite/tests/`。
当前该测试目录尚未创建，不把未实现的测试命令列为已通过验证。

## 日常维护规则

1. 先确定代码所有者：产品策略、共享执行、具体适配器或协议。
2. 新依赖写入实际使用它的包 manifest，再在根更新并提交 `uv.lock` / `pnpm-lock.yaml`。
3. 公共协议变更同时更新生产者、消费者、示例和契约测试；禁止从 Enterprise 导入 DTO 作为跨产品复用。
4. 改文档时标明“已实现 / 设计约定 / 后续计划”，避免把设计命令当成可运行入口。
5. 独立产品发行前从 wheel 干净安装；workspace 中可导入不等于依赖声明完整。
