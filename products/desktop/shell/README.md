# Desktop shell

Electron 主进程已经可以启动 React/Vite renderer，并管理本地 Python service 的启动、握手、健康检查、重启和退出回收。

当前结构：

- `src/main/`：安全窗口和 sidecar supervisor；菜单、凭据和更新尚未实现。
- `src/preload/`：contextBridge 只暴露服务状态与重启。
- `src/shared/`：握手和 IPC 状态类型，以及握手的运行时校验。
- `scripts/dev.mjs`：启动 Vite、编译 shell 并打开 Electron。

shell 不依赖其他产品，不执行 Agent 算法。开发环境启动 workspace `.venv` 中的 Python；发行包内的冻结 sidecar 和打包配置仍待实现。
需要 Node 的 MCP/Skill 运行时须显式打包或验证，不能假设 Electron 内置 Node 自动等价于系统 node 命令。

[进程与 bridge 契约](../../../docs/products/desktop/host-contract.md)
