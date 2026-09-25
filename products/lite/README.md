# Covalent Lite

面向外部 AI-native 应用的轻量 Agent 运行产品：CLI 优先，提供小而稳定的 HTTP/SSE 接口。

**当前状态：目录与可安装 Python 包骨架；尚无可运行的 Lite CLI、HTTP 服务或配置加载器。**
Enterprise 的 `main.py` 和 `dev.sh` 仍是 Enterprise 入口。

- [开发指南与模块归属](../../docs/products/lite/development.md)
- [首版功能与接口约定](../../docs/products/lite/contracts.md)
- [Monorepo 开发导航](../../docs/development.md)

`service/` 是独立分发包 `covalent-lite`，命名空间为 `covalent_lite`。
当前仅声明 contracts/runtime 依赖；实现装配时再增加 agent-kit 与 CLI/HTTP 依赖。
不依赖 Enterprise、数据库、桌面壳或 Monitor。维护职责归 Lite 产品维护者；
共享执行语义由 Runtime 维护者评审，公共协议变更同时邀请 Enterprise 维护者。

骨架验证（仓库根目录，Python 3.12+）：

```sh
uv sync --package covalent-lite
uv run --package covalent-lite python -c "import covalent_lite"
uv build --package covalent-lite --out-dir /tmp/covalent-lite-dist
uv run python -m pytest tests/architecture/test_monorepo.py
```

这些命令验证包结构，不代表 Agent 功能可用。开发 Enterprise 时执行 `uv sync` 恢复其依赖环境。
