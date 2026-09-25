# Covalent Desktop

面向 macOS 和 Windows 的个人 Agent 工作台。技术方向为 Electron + React/Vite + Python sidecar，复用 Covalent 的共享执行内核。

**当前状态：项目结构、workspace 包和开发文档已建立。尚无可启动的桌面窗口、sidecar 服务、安装包或业务 UI。**
Electron、Vite 和本地存储依赖将在对应功能切片中加入并锁定版本；不提供占位启动命令。

| 目录 | 职责 |
| --- | --- |
| `shell/` | Electron 主进程、preload、系统能力与 sidecar 生命周期 |
| `web/` | React 工作台、对话和资源管理；不依赖 Next.js 服务端 |
| `service/` | 独立 Python 包 covalent-desktop；应用用例、本地 API、基础设施装配 |
| `packaging/` | macOS/Windows sidecar、签名、安装与更新验收 |
| `tests/` | 桌面产品的集成、生命周期和双平台测试规划 |

开发顺序见 [开发指南](../../docs/products/desktop/development.md)，进程通信见 [宿主与服务契约](../../docs/products/desktop/host-contract.md)。
跨产品约束见 [内核一致性规范](../../docs/runtime-consistency.md)，选型依据见 [ADR](../../docs/adr/0001-desktop-stack.md)。

## 当前可用的验证命令

在仓库根目录执行，Python 3.12+，uv；Node/pnpm 遵循根 package.json。

```sh
uv sync --locked --package covalent-desktop
uv run --package covalent-desktop python -c "import covalent_desktop"
uv build --package covalent-desktop --wheel
uv run python -m pytest tests/architecture/test_monorepo.py
pnpm install --frozen-lockfile
```

`uv sync` 可恢复默认 Enterprise 开发环境。现有 `main.py`、`dev.sh`、`pnpm dev:enterprise` 仍是 Enterprise 入口。
产品骨架只依赖 contracts/runtime；当前没有 Desktop 的公开 Python API 或 console script。
独立 wheel 安装检查已经纳入 tooling/verify_wheels.py，按该脚本说明先构建全部 wheel。

维护职责：Desktop 维护者负责壳、交互、本地数据和发行；Runtime 维护者负责执行语义；公共模板/调用协议变更需受影响产品维护者共同评审。
