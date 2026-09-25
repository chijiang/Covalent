# Desktop renderer

当前已有 React/Vite 状态页，可以显示 Python service 的启动状态、版本、协议和进程号，并从受限 bridge 请求重启。

后续建立 `src/app/`（路由和壳）、`src/workspaces/`（Agent/Chat/资源工作区）、
`src/services/`（typed bridge adapter）。与 Enterprise 共用的组件先提取到 packages/typescript，
禁止直接导入 products/enterprise/web。

保持现有浅色、红色强调、多面板的工作台语言。共享组件通过 props/services 注入行为，
不依赖 Electron、Next.js 路由或企业身份。Renderer 不保存长期服务凭据，不直接管理 Python 进程。

[开发指南](../../../docs/products/desktop/development.md)
