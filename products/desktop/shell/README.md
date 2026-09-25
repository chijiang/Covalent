# Desktop shell

当前仅有 workspace manifest，没有 Electron 安装依赖或可执行入口。

实施时建立：

- `src/main/`：窗口、菜单、系统对话框、凭据访问、更新和 sidecar supervisor。
- `src/preload/`：contextBridge 暴露的窄接口；不透传任意 IPC 或 shell 命令。
- `src/contracts/`：桌面私有 IPC 类型和运行时校验；跨产品调用类型从公共 schema 生成。

Electron 与打包工具在第一功能切片选定版本并加入本包。shell 不依赖其他产品，不执行 Agent 算法。
需要 Node 的 MCP/Skill 运行时须显式打包或验证，不能假设 Electron 内置 Node 自动等价于系统 node 命令。

[进程与 bridge 契约](../../../docs/products/desktop/host-contract.md)
