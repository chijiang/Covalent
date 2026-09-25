# Covalent Desktop

面向 macOS 和 Windows 的个人 Agent 工作台。技术方向为 Electron + React/Vite + Python sidecar，复用 Covalent 的共享执行内核。

**当前状态：本地开发链路已可运行。** Electron 窗口、React/Vite 状态页、Python sidecar、随机令牌鉴权、协议握手、健康检查、重启和退出清理已经实现。
Agent 调用、本地存储、模板交换、冻结 sidecar 和 macOS/Windows 安装包尚未实现。

| 目录 | 职责 |
| --- | --- |
| `shell/` | Electron 主进程、preload、系统能力与 sidecar 生命周期 |
| `web/` | React 工作台、对话和资源管理；不依赖 Next.js 服务端 |
| `service/` | 独立 Python 包 covalent-desktop；应用用例、本地 API、基础设施装配 |
| `packaging/` | macOS/Windows sidecar、签名、安装与更新验收 |
| `tests/` | 桌面产品的集成、生命周期和双平台测试规划 |

开发顺序见 [开发指南](../../docs/products/desktop/development.md)，进程通信见 [宿主与服务契约](../../docs/products/desktop/host-contract.md)。
跨产品约束见 [内核一致性规范](../../docs/runtime-consistency.md)，选型依据见 [ADR](../../docs/adr/0001-desktop-stack.md)。

## 本地启动

在仓库根目录执行，Python 3.12+，uv；Node/pnpm 遵循根 package.json。

```sh
uv sync --locked --all-packages
pnpm install --frozen-lockfile
pnpm dev:desktop
```

`dev:desktop` 使用 Vite 热更新 renderer；修改 Electron main/preload 后重新启动命令。开发环境直接使用 workspace `.venv` 中的 Python；可用 `COVALENT_DESKTOP_PYTHON` 指定其他解释器。
一次性构建并验证完整本地链路：

```sh
pnpm typecheck:desktop
uv run --package covalent-desktop python -m pytest products/desktop/tests
pnpm smoke:desktop
```

smoke 会打开窗口，等待 renderer 显示 service ready，再关闭窗口并确认 sidecar 正常退出。当前已在 macOS arm64 实测；Windows 由新增的 CI job 检查源码测试与构建，真实安装包仍待验证。
现有 `main.py`、`dev.sh`、`pnpm dev:enterprise` 仍是 Enterprise 入口。service 公开的 `covalent-desktop-service` 仅供宿主启动和诊断，不是用户侧 Agent CLI。
如果只执行过 `uv sync --package covalent-desktop`，运行 Enterprise 前用 `uv sync` 恢复默认环境；同时开发多个产品优先使用 `uv sync --all-packages`。

维护职责：Desktop 维护者负责壳、交互、本地数据和发行；Runtime 维护者负责执行语义；公共模板/调用协议变更需受影响产品维护者共同评审。
